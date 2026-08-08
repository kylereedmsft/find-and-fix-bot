"""Tests for orchestration, `git apply`, and fix application (`findfix.app`).

These stub the Copilot investigator so no live model / auth is needed — the
harness owns everything except the judgment call, and that's exactly the seam
we mock here.
"""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from findfix.app import FindFixApp, _git_apply
from findfix.config import WorkConfig
from findfix.models import Resolution, Verdict


def _git_init(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def test_git_apply_success(tmp_path):
    _git_init(tmp_path)
    (tmp_path / "f.py").write_text("a\nb\nc\n", encoding="utf-8")
    diff = "--- a/f.py\n+++ b/f.py\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n"
    ok, msg = _git_apply(tmp_path, diff)
    assert ok, msg
    assert (tmp_path / "f.py").read_text() == "a\nB\nc\n"


def test_git_apply_recount_tolerates_wrong_counts(tmp_path):
    _git_init(tmp_path)
    (tmp_path / "f.py").write_text("a\nb\nc\nd\n", encoding="utf-8")
    # deliberately wrong hunk counts — --recount should still apply it
    diff = "--- a/f.py\n+++ b/f.py\n@@ -1,9 +1,9 @@\n a\n-b\n+B\n c\n d\n"
    ok, _ = _git_apply(tmp_path, diff)
    assert ok
    assert (tmp_path / "f.py").read_text() == "a\nB\nc\nd\n"


def test_git_apply_empty_diff(tmp_path):
    ok, msg = _git_apply(tmp_path, "   ")
    assert not ok and "empty" in msg


def test_git_apply_non_applicable(tmp_path):
    _git_init(tmp_path)
    (tmp_path / "f.py").write_text("totally different\n", encoding="utf-8")
    diff = "--- a/f.py\n+++ b/f.py\n@@ -1,1 +1,1 @@\n-nonexistent line\n+x\n"
    ok, _ = _git_apply(tmp_path, diff)
    assert not ok


def _make_app(tmp_path, resolution: Resolution) -> FindFixApp:
    _git_init(tmp_path)
    (tmp_path / "s.py").write_text(
        "def f():\n    try:\n        risky()\n    except:\n        pass\n", encoding="utf-8"
    )
    w = WorkConfig(
        label="t", root=str(tmp_path), include=("**/*.py",),
        regex=r"except\s*:", refiner="line-window",
    )
    app = FindFixApp(w, interval=1)

    async def fake_investigate(_m):
        return resolution

    app._investigator.investigate = fake_investigate  # type: ignore[assignment]
    return app


def test_scan_seeds_and_investigates(tmp_path):
    res = Resolution(verdict=Verdict.FIX, explanation="bare except", diff="d")
    app = _make_app(tmp_path, res)
    asyncio.run(app._scan_once())
    items = list(app.state.items.values())
    assert len(items) == 1
    assert items[0].verdict == Verdict.FIX


def test_scan_via_process_pool(tmp_path):
    """The production offload path: run the scan in a subprocess pool and
    verify matches come back and the pool is cleaned up by stop()."""
    res = Resolution(verdict=Verdict.FIX, explanation="e", diff="d")
    app = _make_app(tmp_path, res)
    app._scan_in_process = True  # opt in despite the suite-wide thread default
    try:
        asyncio.run(app._scan_once())
        assert app._scan_pool is not None  # pool was created and used
        items = list(app.state.items.values())
        assert len(items) == 1
        assert items[0].verdict == Verdict.FIX
    finally:
        app.stop()
        assert app._scan_pool is None  # stop() tore the pool down


def test_apply_fix_writes_and_marks_applied(tmp_path):
    diff = (
        "--- a/s.py\n+++ b/s.py\n@@ -2,4 +2,4 @@ def f():\n"
        "     try:\n         risky()\n-    except:\n+    except Exception:\n         pass\n"
    )
    app = _make_app(tmp_path, Resolution(verdict=Verdict.FIX, explanation="e", diff=diff))
    asyncio.run(app._scan_once())
    key = next(iter(app.state.items))
    ok, msg = app.apply_fix(key)
    assert ok, msg
    assert app.state.items[key].verdict == Verdict.APPLIED
    assert "except Exception:" in (tmp_path / "s.py").read_text()


def test_apply_fix_no_diff_is_noop(tmp_path):
    app = _make_app(tmp_path, Resolution(verdict=Verdict.SKIP, explanation="false positive"))
    asyncio.run(app._scan_once())
    key = next(iter(app.state.items))
    ok, msg = app.apply_fix(key)
    assert not ok and "no applicable fix" in msg


def test_cached_result_reused_no_second_investigation(tmp_path):
    """A second app instance must hydrate from cache and not re-investigate."""
    res = Resolution(verdict=Verdict.FIX, explanation="e", diff="d")
    app = _make_app(tmp_path, res)
    asyncio.run(app._scan_once())

    # New app, same work/root. Investigator raises if called — proving cache hit.
    w = app.work
    app2 = FindFixApp(w, interval=1)
    called = {"n": 0}

    async def boom(_m):
        called["n"] += 1
        raise AssertionError("should not investigate a cached match")

    app2._investigator.investigate = boom  # type: ignore[assignment]
    asyncio.run(app2._scan_once())
    assert called["n"] == 0
    assert next(iter(app2.state.items.values())).verdict == Verdict.FIX


def test_paused_analysis_seeds_but_skips_investigation(tmp_path):
    app = _make_app(tmp_path, Resolution(verdict=Verdict.FIX, explanation="e", diff="d"))
    app.state.analysis_enabled = False
    asyncio.run(app._scan_once())
    # match is seeded (visible) but left PENDING because analysis is paused
    items = list(app.state.items.values())
    assert len(items) == 1
    assert items[0].verdict == Verdict.PENDING


def test_reevaluate_forces_fresh_investigation(tmp_path):
    calls = {"n": 0}
    seq = [
        Resolution(verdict=Verdict.SKIP, explanation="first"),
        Resolution(verdict=Verdict.FIX, explanation="second", diff="d"),
    ]
    app = _make_app(tmp_path, seq[0])

    async def stub(_m):
        i = min(calls["n"], len(seq) - 1)
        calls["n"] += 1
        return seq[i]

    app._investigator.investigate = stub  # type: ignore[assignment]
    asyncio.run(app._scan_once())
    key = next(iter(app.state.items))
    assert app.state.items[key].verdict == Verdict.SKIP
    assert calls["n"] == 1

    ok, _ = asyncio.run(app.reevaluate(key))
    assert ok
    assert calls["n"] == 2  # ignored the cache, ran again
    assert app.state.items[key].verdict == Verdict.FIX


def test_reevaluate_runs_even_when_paused(tmp_path):
    app = _make_app(tmp_path, Resolution(verdict=Verdict.FIX, explanation="e", diff="d"))
    asyncio.run(app._scan_once())
    key = next(iter(app.state.items))
    app.state.analysis_enabled = False  # paused
    called = {"n": 0}

    async def stub(_m):
        called["n"] += 1
        return Resolution(verdict=Verdict.SKIP, explanation="re-run")

    app._investigator.investigate = stub  # type: ignore[assignment]
    ok, _ = asyncio.run(app.reevaluate(key))
    assert ok and called["n"] == 1  # explicit action ignores pause


def test_restart_clears_items_and_cache_and_wakes(tmp_path):
    """'Re-eval tab' wipes the tab's results + cache and wakes the find loop so
    the next cycle re-scans and re-investigates from scratch."""
    app = _make_app(tmp_path, Resolution(verdict=Verdict.FIX, explanation="e", diff="d"))
    asyncio.run(app._scan_once())
    assert app.state.items                 # seeded
    assert app._cache.hydrate()            # and cached to disk

    app._wake.clear()
    asyncio.run(app.restart())
    assert app.state.items == {}           # in-memory results cleared
    assert app._cache.hydrate() == {}      # persisted cache wiped
    assert app._wake.is_set()              # loop woken to re-scan
    assert app._force_discovery is True

    # A fresh scan re-seeds and re-investigates the same match from scratch.
    asyncio.run(app._scan_once())
    assert len(app.state.items) == 1
    assert next(iter(app.state.items.values())).verdict == Verdict.FIX


def test_restart_skips_while_scanning(tmp_path):
    """restart() must not clear state mid-scan (it would race the cycle that's
    mutating state.items)."""
    app = _make_app(tmp_path, Resolution(verdict=Verdict.FIX, explanation="e", diff="d"))
    asyncio.run(app._scan_once())
    app.state.scanning = True
    app._wake.clear()
    asyncio.run(app.restart())
    assert app.state.items          # untouched
    assert not app._wake.is_set()   # no-op


def test_file_change_outside_focus_marks_stale(tmp_path):
    _git_init(tmp_path)
    lines = ["# header"] + [f"x{i} = {i}" for i in range(30)]
    lines[20] = "    except:"  # a match deep in the file
    (tmp_path / "s.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    w = WorkConfig(
        label="t", root=str(tmp_path), include=("**/*.py",),
        regex=r"except\s*:", refiner="line-window", context_lines=1,
    )
    app = FindFixApp(w, interval=1)

    async def stub(_m):
        return Resolution(verdict=Verdict.SKIP, explanation="e")

    app._investigator.investigate = stub  # type: ignore[assignment]
    asyncio.run(app._scan_once())
    key = next(iter(app.state.items))
    assert not app.state.items[key].stale

    # change a line far outside the ±1 focus window; match key stays the same
    lines[0] = "# header CHANGED"
    (tmp_path / "s.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    asyncio.run(app._scan_once())
    assert key in app.state.items  # focus unchanged => same key, served from cache
    assert app.state.items[key].stale  # but flagged because the file changed


def test_file_sig_persists_across_cache_roundtrip(tmp_path):
    res = Resolution(verdict=Verdict.SKIP, explanation="e")
    app = _make_app(tmp_path, res)
    asyncio.run(app._scan_once())
    key = next(iter(app.state.items))
    assert app.state.items[key].resolution.file_sig  # captured

    app2 = FindFixApp(app.work, interval=1)  # hydrates from disk
    assert app2.state.items[key].resolution.file_sig == app.state.items[key].resolution.file_sig


def test_hydrated_match_key_matches_dict_key(tmp_path):
    """Regression: a match hydrated from cache must reproduce the exact key it
    was stored under. The key embeds the focus content-hash, which the cache
    does not re-derive (focus_code isn't persisted), so it must be persisted
    separately and restored into `stored_hash`. Otherwise the detail pane can't
    resolve the selected row (items.get(sel) misses)."""
    res = Resolution(verdict=Verdict.FIX, explanation="e", diff="d")
    app = _make_app(tmp_path, res)
    asyncio.run(app._scan_once())
    assert app.state.items  # sanity

    app2 = FindFixApp(app.work, interval=1)  # hydrates purely from disk
    assert app2.state.items
    for k, a in app2.state.items.items():
        assert a.match.key == k, f"hydrated key {a.match.key!r} != dict key {k!r}"



    """A folder excluded after items were cached (even APPLIED) must disappear."""
    _git_init(tmp_path)
    (tmp_path / "dlc").mkdir()
    (tmp_path / "dlc" / "s.py").write_text("try:\n    x()\nexcept:\n    pass\n", encoding="utf-8")
    (tmp_path / "keep.py").write_text("try:\n    x()\nexcept:\n    pass\n", encoding="utf-8")
    w = WorkConfig(
        label="t", root=str(tmp_path), include=("**/*.py",),
        regex=r"except\s*:", refiner="line-window",
    )
    app = FindFixApp(w, interval=1)

    async def stub(_m):
        return Resolution(verdict=Verdict.FIX, explanation="e", diff="d")

    app._investigator.investigate = stub  # type: ignore[assignment]
    asyncio.run(app._scan_once())
    paths = {a.match.path for a in app.state.items.values()}
    assert "dlc/s.py" in paths and "keep.py" in paths
    # mark the dlc one APPLIED to prove even applied items are dropped
    dlc_key = next(k for k, a in app.state.items.items() if a.match.path == "dlc/s.py")
    app.state.items[dlc_key].resolution.verdict = Verdict.APPLIED

    # now exclude dlc and re-scan (simulates editing the config)
    w2 = WorkConfig(
        label="t", root=str(tmp_path), include=("**/*.py",), exclude=("dlc/**",),
        regex=r"except\s*:", refiner="line-window",
    )
    app2 = FindFixApp(w2, interval=1)  # same scope_key (exclude not in it) => hydrates dlc
    assert any(a.match.path == "dlc/s.py" for a in app2.state.items.values()) is False  # dropped at hydrate
    app2._investigator.investigate = stub  # type: ignore[assignment]
    asyncio.run(app2._scan_once())
    paths2 = {a.match.path for a in app2.state.items.values()}
    assert "dlc/s.py" not in paths2
    assert "keep.py" in paths2
