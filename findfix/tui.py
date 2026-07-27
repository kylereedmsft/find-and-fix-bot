"""
PRESENTATION — Textual TUI.

One tab per unit of work. Table: the work unit's matches with a find/fix
status. Detail pane: the LLM's explanation plus the proposed unified diff.
`a` applies the selected fix to the working tree.

Pure view code — reads `AppState`, never touches the scanner or Copilot
directly (fix application is delegated to `FindFixApp.apply_fix`).
"""

from __future__ import annotations

import os
import asyncio
from datetime import datetime, timezone

from rich.console import Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    OptionList,
    Static,
    Tab,
    Tabs,
    TextArea,
)

from .app import FindFixApp
from .models import AnalyzedMatch, Verdict

_SPINNER = "|/-\\"


def _flag(v: Verdict) -> Text:
    return Text(f" {v.glyph} ", style=v.style)


def _detail_renderable(a: AnalyzedMatch | None):
    if a is None:
        return Panel(Text("Select a match", style="dim"), title="Details")

    m, r = a.match, a.resolution
    header = Text()
    header.append(_flag(r.verdict))
    header.append(f"  {m.path}:{m.line}", style="bold")
    header.append(f"   [{m.work}]", style="magenta")
    if r.confidence:
        header.append(f"   confidence: {r.confidence}", style="dim")
    header.append(f"\nfocus: lines {m.focus_start}-{m.focus_end} via {m.refiner}", style="dim")
    if a.stale:
        header.append(
            "\n⟳ file changed on disk since this was evaluated — press 'e' to re-evaluate",
            style="bold yellow",
        )
    if m.occurrence_count > 1:
        shown = ", ".join(str(x) for x in m.occurrences[:20])
        more = "…" if m.occurrence_count > 20 else ""
        header.append(
            f"\n{m.occurrence_count} occurrences in this file (fixed together): lines {shown}{more}",
            style="dim",
        )
    if m.matched_text:
        header.append(f"\nmatched: {m.matched_text[:120]}", style="dim")
    if r.is_tracked:
        header.append(f"\n⧉ tracked as work item #{r.work_item_id}", style="bold cyan")

    if r.verdict == Verdict.PENDING:
        return Panel(Group(header, Text("\nCopilot investigating…", style="yellow")), title="Details")
    if r.verdict == Verdict.ERROR:
        return Panel(
            Group(header, Text(f"\nInvestigation failed: {r.error}", style="bold red")),
            title="Details",
        )

    body: list = [header, Text("")]
    if r.explanation:
        body += [Markdown(f"**Explanation** — {r.explanation}"), Text("")]

    if r.verdict == Verdict.APPLIED:
        body.append(Text("✓ Fix applied to the working tree.", style="bold green"))
        body.append(Text(""))
    if r.diff.strip():
        body.append(Text("Proposed fix:" if r.verdict == Verdict.FIX else "Fix diff:", style="bold"))
        body.append(Syntax(r.diff, "diff", theme="ansi_dark", word_wrap=True))
        if r.verdict == Verdict.FIX:
            body.append(Text("\nPress 'a' to apply this fix.", style="dim"))
    elif r.verdict == Verdict.SKIP:
        body.append(Text("No fix needed (skipped).", style="dim"))

    return Panel(Group(*body), title="Details")


class ThemePicker(ModalScreen[None]):
    BINDINGS = [("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    ThemePicker { align: center middle; }
    ThemePicker > OptionList { width: 40; max-height: 70%; border: round $accent; padding: 0 1; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._original = ""

    def compose(self) -> ComposeResult:
        yield OptionList(id="themes")

    def on_mount(self) -> None:
        self._original = self.app.theme
        ol = self.query_one("#themes", OptionList)
        names = sorted(self.app.available_themes)
        for name in names:
            ol.add_option(name)
        if self._original in names:
            ol.highlighted = names.index(self._original)
        ol.focus()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self.app.theme = str(event.option.prompt)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.app.theme = str(event.option.prompt)
        self.dismiss()

    def action_cancel(self) -> None:
        self.app.theme = self._original
        self.dismiss()


class HeaderBar(Static):
    def __init__(self, tui: "FindFixTUI") -> None:
        super().__init__()
        self._tui = tui

    def render(self):
        s = self._tui.active.state
        line = Text(no_wrap=True, overflow="ellipsis")
        line.append(" find-and-fix ", style="bold white on blue")
        line.append("  copilot-sdk   ", style="dim")
        if not s.analysis_enabled:
            line.append(
                f"analysis PAUSED ({s.pending_count} pending) — SPACE to resume   ",
                style="bold black on yellow",
            )
        if s.scanning:
            line.append("~ " + s.status, style="yellow")
        elif s.error:
            line.append("x " + s.error.replace("\n", " ")[:200], style="bold red")
        else:
            line.append(s.status, style="green")
        return line


class TabStatus(Static):
    def __init__(self, tui: "FindFixTUI") -> None:
        super().__init__()
        self._tui = tui

    def render(self):
        a = self._tui.active
        s = a.state
        parts: list[tuple[str, str]] = [(f"{len(s.items)} match", "bold")]
        if s.fix_count:
            parts.append((f"{s.fix_count} fix", "bold red"))
        if s.applied_count:
            parts.append((f"{s.applied_count} applied", "green"))
        if s.pending_count:
            parts.append((f"{s.pending_count} pending", "yellow"))
        if not s.analysis_enabled:
            parts.append(("PAUSED", "bold black on yellow"))
        if s.last_refresh:
            age = int((datetime.now(timezone.utc) - s.last_refresh).total_seconds())
            parts.append((f"next {max(0, a.interval - age)}s", "dim"))
        t = Text(no_wrap=True, justify="right")
        for i, (txt, style) in enumerate(parts):
            if i:
                t.append("  ·  ", style="dim")
            t.append(txt, style=style)
        t.append(" ")
        return t


class ChatScreen(ModalScreen[None]):
    """Interactive discussion about one match.

    Talks to the match's persistent `ChatSession` (via
    `FindFixApp.chat_session`). The context (pattern, code, current proposed
    fix, unit guidance) is preloaded lazily on the first message. Feedback that
    asks for a revised fix causes the model to emit a fix block that the harness
    applies to the proposal — surfaced here as a `[harness]` line. Esc closes
    the view; the session and its transcript persist.
    """

    BINDINGS = [
        Binding("enter", "send", "Send", priority=True),
        Binding("escape", "close", "Close", priority=True),
        Binding("ctrl+y", "copy", "Copy"),
    ]
    DEFAULT_CSS = """
    ChatScreen { align: center middle; }
    #chat_box { width: 90%; height: 90%; border: round $accent; background: $surface; }
    #chat_scroll { height: 1fr; padding: 0 1; }
    #chat_log { width: 1fr; }
    #ask { height: 6; border: round $primary; }
    #chat_buttons { height: 3; align-horizontal: right; }
    #chat_buttons Button { margin: 0 1; }
    """

    def __init__(self, app: "FindFixApp", key: str, title: str) -> None:
        super().__init__()
        self._source = app
        self._key = key
        self._title = title
        self._session = None
        self._queued: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="chat_box"):
            yield Static(Text(self._title, style="bold"), id="chat_title")
            with VerticalScroll(id="chat_scroll"):
                yield Static("", id="chat_log")
            yield TextArea(id="ask")
            with Horizontal(id="chat_buttons"):
                yield Button("Send  (Enter)", id="send", variant="primary")
                yield Button("Copy", id="copy")
                yield Button("Close  (Esc)", id="close")

    def on_mount(self) -> None:
        self.query_one("#ask", TextArea).focus()
        self._redraw()
        self.run_worker(self._connect(), exclusive=False)

    async def _connect(self) -> None:
        self._redraw(connecting=True)
        session = await self._source.chat_session(self._key)
        if not self.is_mounted:  # dismissed while connecting
            return
        if session is None:
            self.query_one("#chat_log", Static).update(
                Text("(no session available for this match)", style="red")
            )
            return
        self._session = session
        session.on_update = self._on_update
        self._redraw()
        for msg in self._queued:
            self.run_worker(session.ask(msg), exclusive=False)
        self._queued.clear()

    def _on_update(self) -> None:
        if self.is_mounted:
            self.app.call_later(self._redraw)

    def _redraw(self, connecting: bool = False) -> None:
        # The screen may have been dismissed (Esc) while a background
        # `_connect`/`ask` was in flight, or an async session update may land
        # after close — in which case our widgets are gone. Bail out safely.
        if not self.is_mounted:
            return
        try:
            log = self.query_one("#chat_log", Static)
        except NoMatches:
            return
        blocks: list = []
        transcript = self._session.transcript if self._session else []
        for role, text in transcript:
            if role == "you":
                blocks.append(Text(f"You: {text}", style="bold cyan"))
            elif role == "sys":
                blocks.append(Text(f"[harness] {text}", style="italic green"))
            else:
                blocks.append(Markdown(text))
            blocks.append(Text(""))
        if self._session is None and connecting:
            blocks.append(Text("connecting…", style="dim"))
        elif self._session is not None and self._session.pending:
            blocks.append(Text("thinking…", style="yellow"))
        elif not transcript:
            blocks.append(
                Text(
                    "Ask about this match or request a change to the fix "
                    "(e.g. \"use the typed GetOrAdd<T> extension instead of a cast\").",
                    style="dim",
                )
            )
        log.update(Group(*blocks) if blocks else Text(""))
        try:
            self.query_one("#chat_scroll", VerticalScroll).scroll_end(animate=False)
        except Exception:  # noqa: BLE001
            pass

    def _send(self) -> None:
        ta = self.query_one("#ask", TextArea)
        text = ta.text.strip()
        if not text:
            return
        ta.text = ""
        if self._session is None:
            self._queued.append(text)
            self._redraw(connecting=True)
            return
        self.run_worker(self._session.ask(text), exclusive=False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "send":
            self._send()
        elif event.button.id == "copy":
            self.action_copy()
        elif event.button.id == "close":
            self.action_close()

    def action_send(self) -> None:
        self._send()

    def action_copy(self) -> None:
        if self._session is None:
            return
        try:
            import pyperclip  # type: ignore

            pyperclip.copy(self._session.copy_text())
            self.app.notify("Conversation copied.", timeout=3)
        except Exception:  # noqa: BLE001
            self.app.notify("Copy unavailable (pyperclip not installed).", severity="warning")

    def action_close(self) -> None:
        if self._session is not None:
            self._session.on_update = None
        self.dismiss()


class FindFixTUI(App):
    CSS = """
    HeaderBar { height: 1; dock: top; background: $panel; }
    #tabbar { dock: top; height: 2; }
    #works { width: auto; height: 2; }
    #works Underline { display: none; }
    TabStatus { width: 1fr; height: 2; content-align: right middle; }
    #body { height: 1fr; }
    #matches { height: 1fr; border: round $primary; }
    #detail_scroll { height: 4fr; border: round $secondary; }
    DataTable { height: 1fr; }
    """
    BINDINGS = [
        Binding("left", "prev_work", "Prev", priority=True),
        Binding("right", "next_work", "Next", priority=True),
        Binding("space", "toggle_analysis", "Start/pause"),
        Binding("a", "apply_fix", "Apply fix"),
        Binding("e", "reevaluate", "Re-eval"),
        Binding("E", "reevaluate_all", "Re-eval tab"),
        Binding("r", "refresh", "Refresh"),
        Binding("o", "open_file", "Open file"),
        Binding("w", "work_item", "Work item"),
        Binding("d", "discuss", "Discuss"),
        Binding("t", "change_theme", "Theme"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, apps: list[FindFixApp]) -> None:
        super().__init__()
        self.apps = apps
        self.active_index = 0
        self._selected: dict[int, str | None] = {i: None for i in range(len(apps))}
        self._spin = 0
        self._row_index: dict[str, int] = {}
        self._status_col = None

    @property
    def active(self) -> FindFixApp:
        return self.apps[self.active_index]

    def _status_cell(self, a: AnalyzedMatch) -> Text:
        s = self.active.state
        if a.verdict == Verdict.PENDING:
            if not s.analysis_enabled:
                return Text("  paused", style="dim")
            if a.key == s.analysing_key:
                ch = _SPINNER[self._spin % len(_SPINNER)]
                return Text(f"{ch} investigating", style="yellow")
            return Text("  queued", style="dim")
        if a.verdict == Verdict.ERROR:
            return Text("! error", style=a.verdict.style)
        label = {Verdict.FIX: "FIX", Verdict.APPLIED: "applied", Verdict.SKIP: "skip"}[a.verdict]
        return Text(f" {label} ", style=a.verdict.style)

    # ---- layout ----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield HeaderBar(self)
        with Horizontal(id="tabbar"):
            tabs = Tabs(
                *[Tab(self._tab_label(i), id=f"work-{i}") for i in range(len(self.apps))],
                id="works",
            )
            tabs.can_focus = False
            yield tabs
            yield TabStatus(self)
        with Vertical(id="body"):
            yield DataTable(id="matches", cursor_type="row", zebra_stripes=True)
            with VerticalScroll(id="detail_scroll", can_focus=False):
                yield Static(_detail_renderable(None), id="detail")
        yield Footer()

    def _tab_label(self, idx: int) -> Text:
        a = self.apps[idx]
        s = a.state
        pip_style = "dim"
        if s.items:
            worst = min(x.verdict.sort_key for x in s.items.values())
            for x in s.items.values():
                if x.verdict.sort_key == worst:
                    pip_style = x.verdict.style
                    break
        t = Text()
        t.append("● ", style=pip_style)
        t.append(a.work.label, style="bold")
        return t

    def on_mount(self) -> None:
        table = self.query_one("#matches", DataTable)
        keys = table.add_columns("Status", "File", "Line", "Match / reason", "Fix")
        self._status_col = keys[0]
        for i, a in enumerate(self.apps):
            a.on_change(self._make_listener(i))
            self.run_worker(a.run(), exclusive=False, name=f"findfix-{a.work.label}")
        self.set_interval(0.2, self._tick)
        self._refresh_tabs()
        self._refresh_ui()
        table.focus()

    def _make_listener(self, idx: int):
        def listener() -> None:
            self._refresh_tabs()
            if idx == self.active_index:
                self._refresh_ui()
        return listener

    def _refresh_tabs(self) -> None:
        try:
            tabs = self.query_one("#works", Tabs)
        except Exception:  # noqa: BLE001 — not mounted yet
            return
        for i in range(len(self.apps)):
            tab = tabs.query_one(f"#work-{i}", Tab)
            new = self._tab_label(i)
            if tab.label.markup != new.markup:
                tab.label = new
        self.query_one(TabStatus).refresh()

    # ---- data -> table ---------------------------------------------------

    def _refresh_ui(self) -> None:
        table = self.query_one("#matches", DataTable)
        sel = self._selected[self.active_index]
        table.clear()
        row_index: dict[str, int] = {}
        for idx, a in enumerate(self.active.state.ordered()):
            m = a.match
            note = m.matched_text or m.reason or ""
            if m.occurrence_count > 1:
                note = f"{m.occurrence_count}× {note}".strip()
            if a.stale:
                note = f"⟳ stale · {note}".strip(" ·")
            if a.resolution.is_tracked:
                note = f"⧉#{a.resolution.work_item_id} · {note}".strip(" ·")
            fix_cell = (
                Text("diff", style="bold red") if a.resolution.has_fix
                and a.verdict == Verdict.FIX
                else (Text("applied", style="green") if a.verdict == Verdict.APPLIED
                      else Text("-", style="dim"))
            )
            table.add_row(
                self._status_cell(a),
                Text(m.path, overflow="ellipsis"),
                str(m.line),
                Text(note, overflow="ellipsis"),
                fix_cell,
                key=a.key,
            )
            row_index[a.key] = idx
        self._row_index = row_index
        if sel and sel in row_index:
            table.move_cursor(row=row_index[sel])
        elif table.row_count and sel is None:
            table.move_cursor(row=0)

        s = self.active.state
        first_load = not s.items and s.last_refresh is None and s.error is None
        was_loading = table.loading
        table.loading = first_load
        self.query_one("#detail_scroll").loading = first_load
        if was_loading and not first_load:
            table.focus()
        if first_load:
            table.border_title = f" {self.active.work.label} — scanning… "
        elif not s.items:
            table.border_title = f" {self.active.work.label} — no matches "
        else:
            table.border_title = f" {self.active.work.label} — {table.row_count} match(es) "

        self._update_detail()
        self.query_one(HeaderBar).refresh()

    def _tick(self) -> None:
        self._spin += 1
        self.query_one(HeaderBar).refresh()
        self.query_one(TabStatus).refresh()
        aid = self.active.state.analysing_key
        if aid is not None and aid in self._row_index and self._status_col is not None:
            a = self.active.state.items.get(aid)
            if a:
                try:
                    self.query_one("#matches", DataTable).update_cell(
                        aid, self._status_col, self._status_cell(a)
                    )
                except Exception:  # noqa: BLE001
                    pass

    def _update_detail(self) -> None:
        sel = self._selected[self.active_index]
        a = self.active.state.items.get(sel) if sel else None
        if a is None:
            ordered = self.active.state.ordered()
            a = ordered[0] if ordered else None
            if a:
                self._selected[self.active_index] = a.key
        self.query_one("#detail", Static).update(_detail_renderable(a))

    # ---- events ----------------------------------------------------------

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key and event.row_key.value:
            self._selected[self.active_index] = str(event.row_key.value)
            self._update_detail()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key and event.row_key.value:
            self._selected[self.active_index] = str(event.row_key.value)
            self._update_detail()

    def _open_file(self, key: str) -> None:
        a = self.active.state.items.get(key)
        if not a:
            return
        try:
            os.startfile(a.match.abs_path)  # type: ignore[attr-defined]  # Windows
        except Exception:  # noqa: BLE001
            import webbrowser
            webbrowser.open(f"file://{a.match.abs_path}")

    def action_open_file(self) -> None:
        sel = self._selected[self.active_index]
        if sel:
            self._open_file(sel)

    def action_discuss(self) -> None:
        sel = self._selected[self.active_index]
        if not sel:
            return
        a = self.active.state.items.get(sel)
        if a is None:
            return
        title = f"Discuss — {a.match.path}:{a.match.line}  [{self.active.work.label}]"
        self.push_screen(ChatScreen(self.active, sel, title))

    async def action_reevaluate(self) -> None:
        sel = self._selected[self.active_index]
        if not sel:
            return
        ok, msg = await self.active.reevaluate(sel)
        self._refresh_ui()
        self.notify(
            "Re-evaluated." if ok else f"Re-eval failed: {msg}",
            severity="information" if ok else "error",
            timeout=3 if ok else 6,
        )

    async def action_reevaluate_all(self) -> None:
        keys = list(self.active.state.items)
        if not keys:
            return
        self.notify(f"Re-evaluating {len(keys)} item(s)…", timeout=3)
        for key in keys:
            await self.active.reevaluate(key)
        self._refresh_ui()
        self.notify("Re-evaluation complete.", timeout=3)

    def action_apply_fix(self) -> None:
        sel = self._selected[self.active_index]
        if not sel:
            return
        ok, msg = self.active.apply_fix(sel)
        self._refresh_ui()
        if not ok:
            self.notify(f"Apply failed: {msg}", severity="error", timeout=6)
        else:
            self.notify("Fix applied.", severity="information", timeout=3)

    async def action_work_item(self) -> None:
        sel = self._selected[self.active_index]
        if not sel:
            return
        a = self.active.state.items.get(sel)
        if a is None:
            return
        # Already tracked -> open it in the browser instead of re-filing.
        if a.resolution.is_tracked:
            url = a.resolution.work_item_url
            if url:
                import webbrowser
                webbrowser.open(url)
            self.notify(
                f"Already tracked as #{a.resolution.work_item_id}.", timeout=3
            )
            return
        if self.active.work.ado_tracking is None:
            self.notify(
                "ADO tracking not configured for this work unit "
                "(add an 'ado_tracking' block to its config).",
                severity="warning",
                timeout=6,
            )
            return
        self.notify("Filing work item…", timeout=3)
        ok, msg = await asyncio.to_thread(self.active.create_work_item, sel)
        self._refresh_ui()
        self.notify(
            f"Work item {msg}." if ok else f"Work item failed: {msg}",
            severity="information" if ok else "error",
            timeout=3 if ok else 8,
        )

    def _switch_work(self, idx: int) -> None:
        idx = idx % len(self.apps)
        if idx == self.active_index:
            return
        self.active_index = idx
        try:
            self.query_one("#works", Tabs).active = f"work-{idx}"
        except Exception:  # noqa: BLE001
            pass
        self._refresh_ui()
        self.query_one("#matches", DataTable).focus()

    def action_next_work(self) -> None:
        self._switch_work(self.active_index + 1)

    def action_prev_work(self) -> None:
        self._switch_work(self.active_index - 1)

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        tid = event.tab.id or ""
        if tid.startswith("work-"):
            self._switch_work(int(tid.removeprefix("work-")))

    def action_toggle_analysis(self) -> None:
        self.active.toggle_analysis()
        self._refresh_tabs()
        self._refresh_ui()

    async def action_refresh(self) -> None:
        await self.active.refresh_now()

    def action_change_theme(self) -> None:
        self.push_screen(ThemePicker())

    async def action_quit(self) -> None:
        for a in self.apps:
            a.stop()
        await super().action_quit()
