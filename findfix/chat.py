"""Interactive per-match AI discussion ("d" in the TUI).

One `ChatSession` per work item (a `Match`), created lazily on first open and
kept alive for the process lifetime — Esc closes the view, the Copilot session
and transcript persist, and pressing 'd' again resumes exactly where the
conversation left off.

The harness builds the preload context deterministically (the pattern, the unit
context/guidance, the focused code, and the current verdict/explanation/diff);
the session gets the same read-only tool surface as the investigator (Read/Grep
over the work root, skills, and any granted code-intelligence MCP) so the model
can chase code questions live.

When the reviewer is satisfied, the model fences a revised fix as a ```fix
block; the HARNESS parses it and updates the proposed resolution (the model has
no write tools — it proposes, the harness owns application). Transcripts persist
to disk so discussion work is never regenerated from scratch.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Awaitable, Callable

from copilot.session_events import AssistantMessageData, SessionIdleData


def history_path(scope_key: str, key: str) -> Path:
    """Where a chat's transcript persists — survives app restarts."""
    base = (
        Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".cache")
        / "find-and-fix-bot" / "chats"
    )
    safe_scope = re.sub(r"[^\w.-]+", "_", scope_key)
    safe_key = re.sub(r"[^\w.-]+", "_", key)
    return base / safe_scope / f"{safe_key}.json"


# The model requests a fix revision by fencing a payload as ```fix … ``` — the
# HARNESS parses it and updates the resolution (the model has no write tools).
_FIX_RE = re.compile(r"```fix\s*\n(.*?)```", re.DOTALL)

CHAT_SYSTEM = """\
You are the developer's interactive analysis partner inside the find-and-fix
TUI. The first message preloads one candidate match's full context: the pattern
being hunted, the unit's extra guidance/context, the focused source code, and
the fix currently proposed (verdict, explanation, unified diff). The developer
will then discuss it with you — questions, challenges, and requests to revise
the fix (e.g. "use the typed extension methods instead of casts", "make sure all
access to a given key uses the same object", "use the kill-switchable migration
helper").

Ground every claim in source: Read/Grep the working directory and use any
granted code-intelligence MCP (e.g. `search_code`) for anything you can't find
locally; cite file:line. Be concise and technical. Never run code or modify
files — the tools are read-only and the harness applies fixes.

REVISING THE FIX: you cannot edit files yourself. When — and ONLY when — the
developer asks you to change the proposed fix (or agrees to a change you
suggested), emit the complete revised fix as a fenced block:

```fix
{
  "verdict": "fix" | "skip",
  "confidence": "high" | "medium" | "low",
  "explanation": "<what changed and why>",
  "diff": "<full git-style unified diff, or empty string when skip>"
}
```

The diff MUST be a valid git unified diff (`--- a/<path>`, `+++ b/<path>`,
`@@ ... @@` hunks) covering ALL occurrences in the file, with paths relative to
the repo root exactly as in the match. The harness replaces the current proposal
with this one and confirms in the conversation. If the developer is only asking a
question or the request is ambiguous, answer in prose WITHOUT the fence and wait
for an explicit go-ahead."""


class ChatSession:
    """One live Copilot session + its transcript.

    `transcript` is a list of ("you" | "ai" | "sys", text) tuples. `on_update`
    (set by the active view) fires after every transcript change so the UI can
    re-render; it's cleared when the view closes and the session keeps running —
    a reply that lands while no view is open is simply there on resume.
    """

    def __init__(
        self,
        title: str,
        fix_handler: Callable[[str], Awaitable[str]] | None = None,
        history: Path | None = None,
    ) -> None:
        self.title = title
        self.transcript: list[tuple[str, str]] = []
        self.pending: bool = False
        self.on_update: Callable[[], None] | None = None
        self._fix_handler = fix_handler
        self._history = history
        self._session = None
        self._lock = asyncio.Lock()
        self._buf: list[str] = []
        self._idle = asyncio.Event()
        self._context = ""
        self._primed = False
        if history is not None and history.exists():
            try:
                doc = json.loads(history.read_text(encoding="utf-8"))
                self.transcript = [(r, t) for r, t in doc.get("transcript", [])]
            except Exception:  # noqa: BLE001 — corrupt history: start fresh
                pass

    def _save(self) -> None:
        if self._history is None:
            return
        try:
            self._history.parent.mkdir(parents=True, exist_ok=True)
            self._history.write_text(
                json.dumps({"title": self.title, "transcript": self.transcript}),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001 — persistence is best-effort
            pass

    @property
    def started(self) -> bool:
        return self._session is not None

    async def start(self, client, context_prompt: str, **session_kwargs) -> None:
        """Open the Copilot session. The match context is NOT sent yet — it's
        prepended to the first real question, so opening the dialog costs
        nothing. A transcript restored from disk (post-restart) rides along so
        the fresh model session continues the old conversation."""
        self._context = context_prompt
        if self.transcript:
            prior = "\n".join(
                ("Developer: " if r == "you" else "You (AI): " if r == "ai" else "[harness] ")
                + t
                for r, t in self.transcript
            )
            self._context += (
                "\n\nPRIOR CONVERSATION about this match (from a previous "
                "session — continue seamlessly from here):\n" + prior
            )
        self._primed = False
        self._session = await client.create_session(**session_kwargs)

        def on_event(event) -> None:
            match event.data:
                case AssistantMessageData() as d:
                    if d.content:
                        self._buf.append(d.content)
                case SessionIdleData():
                    self._idle.set()

        self._session.on(on_event)

    async def ask(self, text: str, record_user: bool = True) -> None:
        """Send one message; append the reply to the transcript. Sends are
        serialized (the lock is FIFO), but the user's message lands in the
        transcript immediately so asking while the model is thinking queues
        rather than blocks."""
        if record_user:
            self.transcript.append(("you", text))
            self._save()
            self._notify()
        async with self._lock:
            self.pending = True
            self._notify()
            self._buf.clear()
            self._idle.clear()
            outgoing = text
            if not self._primed:
                outgoing = f"PRELOAD:\n{self._context}\n\nDEVELOPER'S MESSAGE:\n{text}"
                self._primed = True
            try:
                await self._session.send(outgoing)
                await asyncio.wait_for(self._idle.wait(), timeout=900)
                reply = "\n".join(self._buf).strip() or "(no reply)"
            except Exception as e:  # noqa: BLE001
                reply = f"(error: {type(e).__name__}: {e})"
            self.transcript.append(("ai", reply))
            self._save()
            self.pending = False
            self._notify()
        # Execute any fix blocks the reply carried (outside the lock so a
        # follow-up question isn't blocked by the update).
        if self._fix_handler is not None:
            for m in _FIX_RE.finditer(reply):
                try:
                    outcome = await self._fix_handler(m.group(1).strip())
                except Exception as e:  # noqa: BLE001
                    outcome = f"fix update failed: {type(e).__name__}: {e}"
                self.transcript.append(("sys", outcome))
                self._save()
                self._notify()

    def _notify(self) -> None:
        if self.on_update is not None:
            try:
                self.on_update()
            except Exception:  # noqa: BLE001 — view may be mid-teardown
                pass

    def copy_text(self) -> str:
        prefix = {"you": "You: ", "ai": "AI: ", "sys": "[harness] "}
        parts = [f"# {self.title}", ""]
        for role, text in self.transcript:
            parts.append(prefix.get(role, "") + text)
            parts.append("")
        return "\n".join(parts)
