"""Headless render regression tests for the TUI.

These use Textual's `run_test` harness to actually mount and render screens,
which catches bugs that pure-logic tests miss — notably method-name collisions
with Textual internals (e.g. a screen defining `_render`, which shadows
`Widget._render` and yields a None visual -> 'NoneType' has no 'render_strips').
"""

from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult
from textual.widgets import Static

from findfix.tui import ChatScreen, FindFixTUI
from findfix.config import WorkConfig
from findfix.models import AnalyzedMatch, AppState, Match, Resolution, Verdict


class _FakeSession:
    def __init__(self, transcript):
        self.transcript = transcript
        self.pending = False
        self.on_update = None
        self.asked = []

    async def ask(self, msg):
        self.asked.append(msg)
        return None

    def copy_text(self):
        return "x"


class _FakeSource:
    def __init__(self, session):
        self._session = session

    async def chat_session(self, key):
        return self._session


def _run_chatscreen(session):
    """Push a ChatScreen over a trivial base app and render it; return any error."""

    class Base(App):
        def compose(self) -> ComposeResult:
            yield Static("base")

        def on_mount(self) -> None:
            self.push_screen(ChatScreen(_FakeSource(session), "k", "Discuss — f.cs:1"))
            self.set_timer(0.4, self.exit)

    async def main():
        async with Base().run_test() as pilot:
            await pilot.pause()
            await pilot.pause()

    asyncio.run(main())


def test_chatscreen_renders_empty_transcript():
    # Regression: ChatScreen must not define `_render` (collides with Widget._render).
    _run_chatscreen(_FakeSession([]))


def test_chatscreen_renders_with_transcript():
    transcript = [
        ("you", "use the typed GetOrAdd<T> extension"),
        ("bot", "**Done.** Here is the revised fix.\n\n```diff\n- a\n+ b\n```"),
        ("sys", "applied revised fix"),
    ]
    _run_chatscreen(_FakeSession(transcript))


def test_chatscreen_renders_no_session():
    # chat_session returns None -> "(no session available)" path must still render.
    _run_chatscreen(None)


def test_chatscreen_redraw_after_dismiss_is_safe():
    """Regression: an async session update (or a `_connect` finishing) after the
    screen is dismissed must not crash with NoMatches on '#chat_log'. `_redraw`
    and `_on_update` must no-op once the screen's widgets are gone."""
    session = _FakeSession([("you", "hi")])
    errors: list = []

    class Base(App):
        def compose(self) -> ComposeResult:
            yield Static("base")

        async def on_mount(self) -> None:
            screen = ChatScreen(_FakeSource(session), "k", "Discuss — f.cs:1")
            await self.push_screen(screen)
            self._screen_ref = screen

    async def main():
        async with Base().run_test() as pilot:
            await pilot.pause()
            screen = pilot.app._screen_ref
            pilot.app.pop_screen()  # dismiss
            await pilot.pause()
            # These fire from background workers/session callbacks post-close.
            try:
                screen._on_update()
                screen._redraw()
                screen._redraw(connecting=True)
            except Exception as e:  # noqa: BLE001
                errors.append(e)
            await pilot.pause()

    asyncio.run(main())
    assert not errors, f"redraw after dismiss raised: {errors}"


class _SlowSource:
    """chat_session resolves only after several event-loop ticks."""

    def __init__(self, session):
        self._session = session

    async def chat_session(self, key):
        for _ in range(5):
            await asyncio.sleep(0.01)
        return self._session


def test_chatscreen_submit_after_slow_connect():
    """Regression: after a slow `_connect`, the screen must set `_session` and a
    typed message pressed with Enter must reach `session.ask` (not get stuck in
    `_queued` forever). Guards must key off widget presence, not `is_mounted`."""
    from textual.widgets import TextArea

    session = _FakeSession([])

    class Base(App):
        def compose(self) -> ComposeResult:
            yield Static("base")

        async def on_mount(self) -> None:
            screen = ChatScreen(_SlowSource(session), "k", "Discuss — f.cs:1")
            await self.push_screen(screen)
            self._screen_ref = screen

    async def main():
        async with Base().run_test() as pilot:
            await pilot.pause()
            screen = pilot.app._screen_ref
            # Wait for the slow connect to finish.
            for _ in range(12):
                await pilot.pause()
            assert screen._session is not None, "connect never set the session"
            screen.query_one("#ask", TextArea).text = "please fix it"
            await pilot.press("enter")  # real binding path
            for _ in range(4):
                await pilot.pause()

    asyncio.run(main())
    assert session.asked == ["please fix it"], f"submit failed, asked={session.asked}"


# --- FindFixTUI: filter + apply-fix cursor behavior -------------------------


def _mk_item(path: str, line: int, verdict: Verdict = Verdict.FIX) -> AnalyzedMatch:
    m = Match(
        work="w", path=path, abs_path=path, line=line, col=1,
        matched_text="IsSPO", snippet="", focus_start=line, focus_end=line,
        focus_code="if (SPFarm.IsSPO) {}",
    )
    diff = "--- a/x\n+++ b/x\n" if verdict in (Verdict.FIX, Verdict.APPLIED) else ""
    return AnalyzedMatch(m, Resolution(verdict=verdict, diff=diff))


class _FakeApp:
    """Minimal stand-in for FindFixApp covering the surface FindFixTUI touches."""

    def __init__(self, items):
        self.work = WorkConfig(label="IsSPO")
        self.state = AppState()
        for a in items:
            self.state.items[a.key] = a
        self._listener = None

    def on_change(self, fn):
        self._listener = fn

    async def run(self):  # worker: idle forever, no scanning
        import asyncio as _a
        await _a.Event().wait()

    def toggle_analysis(self):
        self.state.analysis_enabled = not self.state.analysis_enabled

    def stop(self):
        pass

    def apply_fix(self, key):
        import dataclasses
        a = self.state.items[key]
        self.state.items[key] = dataclasses.replace(
            a, resolution=dataclasses.replace(a.resolution, verdict=Verdict.APPLIED)
        )
        return True, ""


def _run_tui(apps, body):
    """Mount FindFixTUI over `apps`, run `body(pilot)`, return its result."""
    result: dict = {}

    async def main():
        app = FindFixTUI(apps)
        async with app.run_test() as pilot:
            # Wait for compose to finish before the body queries widgets.
            for _ in range(20):
                await pilot.pause()
                try:
                    app.query_one("#matches")
                    app.query_one("#filter")
                    break
                except Exception:  # noqa: BLE001
                    continue
            for _ in range(3):  # let on_mount settle (columns, first refresh)
                await pilot.pause()
            await body(pilot, app, result)

    asyncio.run(main())
    return result


def test_apply_fix_preserves_cursor_position():
    """Applying a fix must keep the cursor at the same LIST POSITION, not follow
    the just-applied item to its new sorted slot (FIX sorts above APPLIED)."""
    from textual.widgets import DataTable

    # Three FIX rows, ordered by path: a.cs, b.cs, c.cs (all sort_key 0).
    items = [_mk_item("a.cs", 1), _mk_item("b.cs", 2), _mk_item("c.cs", 3)]

    async def body(pilot, app, result):
        table = app.query_one("#matches", DataTable)
        table.move_cursor(row=1)  # select b.cs
        await pilot.pause()
        app.action_apply_fix()    # b.cs -> APPLIED, re-sorts below the other FIXes
        await pilot.pause()
        result["cursor_row"] = table.cursor_row
        # Row 1 should now be c.cs (b.cs dropped to the bottom), and the cursor
        # stays on row 1 rather than chasing b.cs.
        result["path_at_cursor"] = str(table.get_row_at(table.cursor_row)[1])

    r = _run_tui([_FakeApp(items)], body)
    assert r["cursor_row"] == 1, f"cursor jumped to row {r['cursor_row']}"
    assert "c.cs" in r["path_at_cursor"], f"cursor landed on {r['path_at_cursor']}"


def test_path_filter_narrows_rows():
    """The path filter shows only rows whose path matches the substring, and
    clearing it restores every row."""
    from textual.widgets import DataTable

    items = [
        _mk_item("sts/stsom/a.cs", 1),
        _mk_item("sts/stsom/b.cs", 2),
        _mk_item("spo/other.cs", 3),
    ]

    async def body(pilot, app, result):
        table = app.query_one("#matches", DataTable)
        result["before"] = table.row_count
        app._filter[app.active_index] = "sts/"
        app._refresh_ui()
        await pilot.pause()
        result["after"] = table.row_count
        app.action_clear_filter()
        await pilot.pause()
        result["cleared"] = table.row_count

    r = _run_tui([_FakeApp(items)], body)
    assert r["before"] == 3
    assert r["after"] == 2, f"filter kept {r['after']} rows, expected 2"
    assert r["cleared"] == 3, "clearing the filter should restore all rows"


def test_filter_input_toggles_and_prefills():
    """'/' shows the filter box prefilled with the tab's current filter."""
    from findfix.tui import FilterInput

    items = [_mk_item("sts/a.cs", 1)]

    async def body(pilot, app, result):
        app._filter[app.active_index] = "sts/"
        app.action_filter()
        await pilot.pause()
        inp = app.query_one("#filter", FilterInput)
        result["visible"] = inp.has_class("visible")
        result["value"] = inp.value

    r = _run_tui([_FakeApp(items)], body)
    assert r["visible"] is True
    assert r["value"] == "sts/"

