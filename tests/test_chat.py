"""Tests for the discuss feature, per-unit context, and chat persistence.

No live Copilot: the chat's Copilot session is faked so we exercise the
transcript/persistence/fix-revision plumbing (`findfix.chat`), context
injection (`findfix.config` + `findfix.investigator`), and the harness-side
fix update (`findfix.app`).
"""

from __future__ import annotations

import asyncio
import subprocess

from copilot.session_events import AssistantMessageData, SessionIdleData

from findfix.app import FindFixApp
from findfix.chat import ChatSession, history_path
from findfix.config import WorkConfig
from findfix.investigator import _SYSTEM, _context_block
from findfix.models import AnalyzedMatch, Match, Resolution, Verdict


# --- per-unit context -------------------------------------------------------

def test_context_changes_scope_key(tmp_path):
    base = dict(label="t", root=str(tmp_path), regex="x", description="d")
    a = WorkConfig(**base)
    b = WorkConfig(**base, context="use typed extensions")
    assert a.scope_key != b.scope_key


def test_context_block_empty_when_unset(tmp_path):
    w = WorkConfig(label="t", root=str(tmp_path), regex="x")
    assert _context_block(w) == ""


def test_context_accepts_list_of_strings(tmp_path):
    from findfix.config import load_work_configs
    import json

    cfg = tmp_path / "c.json"
    cfg.write_text(
        json.dumps({"work": [{
            "label": "t", "root": str(tmp_path), "regex": "x",
            "context": ["rule one", "rule two"],
        }]}),
        encoding="utf-8",
    )
    w = load_work_configs(str(cfg))[0]
    assert w.context == "rule one\nrule two"


def test_context_injected_into_system_prompt(tmp_path):
    w = WorkConfig(
        label="t", root=str(tmp_path), regex="x",
        context="Use the typed GetOrAdd<T> extension instead of a cast.",
    )
    system = _SYSTEM.format(
        label=w.label, regex=w.regex, description="(none)",
        context=_context_block(w), path="f.cs",
    )
    assert "GetOrAdd<T>" in system
    assert "Additional guidance" in system


# --- chat fix parsing / harness update --------------------------------------

def _seed_app(tmp_path, resolution: Resolution) -> tuple[FindFixApp, str]:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "s.cs").write_text("class C { }\n", encoding="utf-8")
    w = WorkConfig(label="t", root=str(tmp_path), include=("**/*.cs",), regex="class")
    app = FindFixApp(w, interval=1)
    m = Match(
        work="t", path="s.cs", abs_path=str(tmp_path / "s.cs"),
        line=1, col=1, matched_text="class", snippet="class C { }",
        focus_start=1, focus_end=1, focus_code="class C { }", refiner="line-window",
    )
    a = AnalyzedMatch(m, resolution)
    app.state.items[a.key] = a
    return app, a.key


def test_apply_chat_fix_replaces_resolution_and_caches(tmp_path):
    app, key = _seed_app(tmp_path, Resolution(verdict=Verdict.SKIP, explanation="old"))
    payload = (
        '{"verdict": "fix", "confidence": "high", '
        '"explanation": "use typed extension", "diff": "--- a/s.cs\\n+++ b/s.cs\\n"}'
    )
    out = asyncio.run(app._apply_chat_fix(key, payload))
    a = app.state.items[key]
    assert a.resolution.verdict == Verdict.FIX
    assert a.resolution.explanation == "use typed extension"
    assert "updated" in out
    # persisted so a re-scan restores the refined resolution
    assert app._cache.get(key) is not None


def test_apply_chat_fix_bad_json(tmp_path):
    app, key = _seed_app(tmp_path, Resolution(verdict=Verdict.FIX, diff="d"))
    out = asyncio.run(app._apply_chat_fix(key, "not json"))
    assert "could not parse" in out


# --- ChatSession with a faked Copilot session -------------------------------

class _FakeEvent:
    def __init__(self, data):
        self.data = data


class _FakeSession:
    """Emits a scripted reply through the registered event callback on send."""

    def __init__(self, reply: str):
        self._reply = reply
        self._cb = None

    def on(self, cb):
        self._cb = cb

    async def send(self, _text: str):
        self._cb(_FakeEvent(AssistantMessageData(content=self._reply, message_id="1")))
        self._cb(_FakeEvent(SessionIdleData()))


class _FakeClient:
    def __init__(self, reply: str):
        self._reply = reply

    async def create_session(self, **_kwargs):
        return _FakeSession(self._reply)


def test_chatsession_fix_block_triggers_handler(tmp_path):
    captured: list[str] = []

    async def handler(payload: str) -> str:
        captured.append(payload)
        return "fix updated"

    reply = 'Sure.\n```fix\n{"verdict": "fix", "diff": "d"}\n```\n'
    cs = ChatSession("t", fix_handler=handler, history=tmp_path / "c.json")

    async def go():
        await cs.start(_FakeClient(reply), "CONTEXT")
        await cs.ask("please revise")

    asyncio.run(go())
    assert len(captured) == 1
    assert '"verdict": "fix"' in captured[0]
    roles = [r for r, _ in cs.transcript]
    assert roles == ["you", "ai", "sys"]  # user, AI reply, harness outcome


def test_chatsession_transcript_persists_and_restores(tmp_path):
    hist = tmp_path / "c.json"
    cs = ChatSession("t", history=hist)

    async def go():
        await cs.start(_FakeClient("hello there"), "CONTEXT")
        await cs.ask("hi")

    asyncio.run(go())
    assert hist.exists()

    restored = ChatSession("t", history=hist)
    assert restored.transcript == cs.transcript
    assert restored.transcript[0] == ("you", "hi")


def test_chatsession_preloads_context_once(tmp_path):
    sent: list[str] = []

    class _RecordingSession(_FakeSession):
        async def send(self, text: str):
            sent.append(text)
            await super().send(text)

    class _RecordingClient:
        async def create_session(self, **_kwargs):
            return _RecordingSession("ok")

    cs = ChatSession("t", history=tmp_path / "c.json")

    async def go():
        await cs.start(_RecordingClient(), "MY-CONTEXT")
        await cs.ask("first")
        await cs.ask("second")

    asyncio.run(go())
    assert "PRELOAD:" in sent[0] and "MY-CONTEXT" in sent[0]
    assert "PRELOAD:" not in sent[1]  # context only rides the first message


def test_history_path_is_scoped(tmp_path):
    p = history_path("label:abc123", "s.cs:1:deadbeef")
    assert "label_abc123" in str(p)
    assert p.name.endswith(".json")
