"""Tests for ADO work-item tracking (`findfix.ado` + app/cache wiring).

The `az` CLI is never invoked here — `findfix.ado.create_work_item` is the seam
we stub, so these run offline with no Azure auth. We cover: config parsing,
title/description builders, the create flow, idempotency (no re-file when an id
is present), a clean failure when `ado_tracking` is unconfigured, and that the
`work_item_id` survives a cache round-trip.
"""

from __future__ import annotations

import asyncio
import subprocess

from findfix.config import AdoTracking, WorkConfig, _coerce
from findfix.models import AnalyzedMatch, Match, Resolution, Verdict
from findfix import ado
from findfix.app import FindFixApp


def _analyzed(path="pkg/s.py", line=4, occ=(4,)) -> AnalyzedMatch:
    m = Match(
        work="t", path=path, abs_path=f"C:/repo/{path}", line=line, col=1,
        matched_text="except:", snippet="…", focus_start=1, focus_end=5,
        focus_code="try:\n    x()\nexcept:\n    pass\n", occurrences=occ,
    )
    r = Resolution(verdict=Verdict.FIX, explanation="bare except", diff="--- a\n+++ b\n")
    return AnalyzedMatch(match=m, resolution=r)


def test_config_parses_ado_tracking():
    w = _coerce({
        "label": "SPO", "regex": "x",
        "ado_tracking": {
            "org": "contoso", "project": "OS", "type": "Bug",
            "area_path": "OS\\Area", "iteration_path": "OS\\Sprint 1",
            "parent": 12345, "tags": "migration", "title_template": "[{label}] {path}",
        },
    })
    t = w.ado_tracking
    assert isinstance(t, AdoTracking)
    assert t.org == "contoso" and t.project == "OS" and t.type == "Bug"
    assert t.parent == 12345 and t.tags == ("migration",)


def test_config_ado_tracking_absent_is_none():
    assert _coerce({"label": "t", "regex": "x"}).ado_tracking is None


def test_build_title_uses_template():
    t = AdoTracking(org="o", project="p", title_template="[{label}] Migrate {path}:{line}")
    title = ado.build_title(t, "SPO", _analyzed())
    assert title == "[SPO] Migrate pkg/s.py:4"


def test_build_title_default_when_no_template():
    t = AdoTracking(org="o", project="p")
    assert ado.build_title(t, "SPO", _analyzed()) == "[SPO] pkg/s.py"


def test_build_description_contains_diff_and_occurrences():
    a = _analyzed(occ=(4, 9, 20))
    body = ado.build_description("SPO", a)
    assert "SPO" in body and "pkg/s.py" in body
    assert "3 occurrences" in body and "4, 9, 20" in body
    assert "<pre>" in body and "bare except" in body


def test_create_work_item_missing_az(monkeypatch):
    monkeypatch.setattr(ado, "_az", lambda: None)
    res = ado.create_work_item(AdoTracking(org="o", project="p"), "t", "d")
    assert not res.ok and "az" in res.message.lower()


def test_create_work_item_parses_id(monkeypatch):
    monkeypatch.setattr(ado, "_az", lambda: "az")
    calls = {"n": 0}

    def fake_run(cmd, **kw):
        calls["n"] += 1
        return subprocess.CompletedProcess(cmd, 0, stdout='{"id": 4242}', stderr="")

    monkeypatch.setattr(ado.subprocess, "run", fake_run)
    res = ado.create_work_item(AdoTracking(org="contoso", project="OS"), "t", "d")
    assert res.ok and res.work_item_id == 4242
    assert "_workitems/edit/4242" in res.url


def test_create_work_item_logged_out(monkeypatch):
    monkeypatch.setattr(ado, "_az", lambda: "az")

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Please run 'az login' to setup account.")

    monkeypatch.setattr(ado.subprocess, "run", fake_run)
    res = ado.create_work_item(AdoTracking(org="o", project="p"), "t", "d")
    assert not res.ok and "az login" in res.message.lower()


def _tracked_app(tmp_path) -> FindFixApp:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "s.py").write_text(
        "def f():\n    try:\n        risky()\n    except:\n        pass\n", encoding="utf-8"
    )
    w = WorkConfig(
        label="t", root=str(tmp_path), include=("**/*.py",),
        regex=r"except\s*:", refiner="line-window",
        ado_tracking=AdoTracking(org="contoso", project="OS"),
    )
    app = FindFixApp(w, interval=1)

    async def fake_investigate(_m):
        return Resolution(verdict=Verdict.FIX, explanation="e", diff="--- a\n+++ b\n")

    app._investigator.investigate = fake_investigate  # type: ignore[assignment]
    return app


def test_create_work_item_notifies_on_loop_thread_not_worker(tmp_path, monkeypatch):
    """Regression: `_notify` (which drives the Textual UI) must never fire from
    the worker thread that runs the blocking `az` call — mutating widgets
    off-thread corrupts the DataTable (RowDoesNotExist on later render). The
    blocking `ado.create_work_item` runs off the loop thread; every `_notify`
    must run ON it."""
    import threading

    loop_thread = threading.get_ident()
    az_threads: list[int] = []
    notify_threads: list[int] = []

    def fake_az(*a, **k):
        az_threads.append(threading.get_ident())
        return ado.WorkItemResult(ok=True, work_item_id=5, url="u", message="created #5")

    monkeypatch.setattr(ado, "create_work_item", fake_az)
    app = _tracked_app(tmp_path)
    app.on_change(lambda: notify_threads.append(threading.get_ident()))
    asyncio.run(app._scan_once())
    key = next(iter(app.state.items))
    notify_threads.clear()

    asyncio.run(app.create_work_item(key))

    assert az_threads and all(t != loop_thread for t in az_threads), "az call must run off-thread"
    assert notify_threads and all(t == loop_thread for t in notify_threads), "notify must stay on the loop thread"


def test_create_work_item_sets_and_persists_id(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ado, "create_work_item",
        lambda *a, **k: ado.WorkItemResult(ok=True, work_item_id=99, url="http://x/99", message="created #99"),
    )
    app = _tracked_app(tmp_path)
    asyncio.run(app._scan_once())
    key = next(iter(app.state.items))
    ok, msg = asyncio.run(app.create_work_item(key))
    assert ok and app.state.items[key].resolution.work_item_id == 99

    # survives a cache round-trip (fresh app hydrates the id from disk)
    app2 = FindFixApp(app.work, interval=1)
    assert app2.state.items[key].resolution.work_item_id == 99
    assert app2.state.items[key].resolution.work_item_url == "http://x/99"


def test_create_work_item_idempotent_when_tracked(tmp_path, monkeypatch):
    calls = {"n": 0}

    def counting(*a, **k):
        calls["n"] += 1
        return ado.WorkItemResult(ok=True, work_item_id=7, url="", message="created #7")

    monkeypatch.setattr(ado, "create_work_item", counting)
    app = _tracked_app(tmp_path)
    asyncio.run(app._scan_once())
    key = next(iter(app.state.items))
    asyncio.run(app.create_work_item(key))
    ok, msg = asyncio.run(app.create_work_item(key))  # second call must not re-file
    assert ok and calls["n"] == 1 and "already tracked" in msg


def test_create_work_item_unconfigured(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "s.py").write_text("try:\n    x()\nexcept:\n    pass\n", encoding="utf-8")
    w = WorkConfig(label="t", root=str(tmp_path), include=("**/*.py",), regex=r"except\s*:")
    app = FindFixApp(w, interval=1)

    async def fake_investigate(_m):
        return Resolution(verdict=Verdict.FIX, explanation="e", diff="d")

    app._investigator.investigate = fake_investigate  # type: ignore[assignment]
    asyncio.run(app._scan_once())
    key = next(iter(app.state.items))
    ok, msg = asyncio.run(app.create_work_item(key))
    assert not ok and "not configured" in msg
