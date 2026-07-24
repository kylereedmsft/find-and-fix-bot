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

from findfix.tui import ChatScreen


class _FakeSession:
    def __init__(self, transcript):
        self.transcript = transcript
        self.pending = False
        self.on_update = None

    async def ask(self, msg):
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
